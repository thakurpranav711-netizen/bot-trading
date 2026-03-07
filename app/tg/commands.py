# app/tg/commands.py

"""
Telegram Command Handlers — Production Grade v2

Available commands (26 total):

📊 Information:
    /start       - Welcome message
    /help        - Show available commands
    /status      - Bot status overview
    /balance     - Current balance & PnL
    /positions   - Open positions with live PnL
    /trades      - Recent trade history
    /performance - Session performance metrics
    /risk        - Risk management report
    /limits      - Trade limiter status
    /market      - Market snapshot for all coins
    /scheduler   - Scheduler status & metrics
    /logs        - Recent log entries
    /alerts      - Pending system alerts

⚙️ Controls:
    /pause       - Pause trading
    /resume      - Resume trading (unlocks all locks)
    /start_bot   - Alias for /resume
    /kill        - Activate kill switch
    /config      - View/update configuration

🔓 Risk Management:
    /unlock      - Unlock risk locks
    /reset_risk  - Accept losses & restart fresh
    /set_risk    - Adjust risk parameters

⚡ Actions:
    /force_exit  - Force close all positions
    /force_cycle - Manually trigger trading cycle
    /close       - Close specific position by symbol
    /buy         - Manual buy order
    /sell        - Manual sell order

Authentication:
    Commands use @require_auth (viewer level) or @require_admin (admin level).
    Viewer: read-only commands (status, balance, positions)
    Admin: write commands (pause, kill, force_exit)
"""

import html
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.utils.logger import get_logger, get_recent_logs, get_pending_alerts
from app.utils.time import (
    format_duration,
    get_uptime_str,
    get_utc_now,
    market_status,
    time_ago,
)
from app.tg.auth import (
    require_auth,
    require_admin,
    get_audit_log,
    get_security_stats,
    AuthRole,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    from telegram.ext import Application

# ── Telegram imports ──────────────────────────────────────────────
try:
    from telegram import Update
    from telegram.ext import CommandHandler, ContextTypes
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = None           # type: ignore
    ContextTypes = None     # type: ignore
    ParseMode = None        # type: ignore


# ═════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═════════════════════════════════════════════════════════════════

SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━"
MAX_MESSAGE_LENGTH = 4000  # Leave room for HTML tags


# ═════════════════════════════════════════════════════════════════
#  SETUP
# ═════════════════════════════════════════════════════════════════

def setup_commands(app: "Application", controller, chat_id: str) -> None:
    """
    Register all command handlers.

    Args:
        app: Telegram Application instance
        controller: BotController instance
        chat_id: Authorized chat ID
    """
    if not TELEGRAM_AVAILABLE:
        logger.warning("Telegram not available — commands not registered")
        return

    # Store references in app context
    app.bot_data["controller"] = controller
    app.bot_data["chat_id"] = chat_id

    # ── Define handlers with auth levels ──────────────────────
    # (name, handler, admin_only)
    handlers = [
        # Basic
        ("start", cmd_start, False),
        ("help", cmd_help, False),

        # Information (viewer level)
        ("status", cmd_status, False),
        ("balance", cmd_balance, False),
        ("positions", cmd_positions, False),
        ("trades", cmd_trades, False),
        ("performance", cmd_performance, False),
        ("risk", cmd_risk, False),
        ("limits", cmd_limits, False),
        ("market", cmd_market, False),
        ("scheduler", cmd_scheduler, False),
        ("logs", cmd_logs, False),
        ("alerts", cmd_alerts, False),

        # Controls (admin level)
        ("pause", cmd_pause, True),
        ("resume", cmd_resume, True),
        ("start_bot", cmd_resume, True),
        ("kill", cmd_kill, True),
        ("config", cmd_config, True),

        # Risk management (admin level)
        ("unlock", cmd_unlock, True),
        ("reset_risk", cmd_reset_risk, True),
        ("set_risk", cmd_set_risk, True),

        # Actions (admin level)
        ("force_exit", cmd_force_exit, True),
        ("force_cycle", cmd_force_cycle, True),
        ("close", cmd_close, True),
        ("buy", cmd_buy, True),
        ("sell", cmd_sell, True),

        # Security (admin level)
        ("security", cmd_security, True),
    ]

    for name, handler, admin_only in handlers:
        app.add_handler(CommandHandler(name, handler))

    logger.info(
        f"Registered {len(handlers)} Telegram commands "
        f"({sum(1 for _, _, a in handlers if a)} admin-only)"
    )


# ═════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════

def _get_controller(context) -> Optional[Any]:
    """Get controller from context."""
    return context.bot_data.get("controller")


def _get_chat_id(context) -> str:
    """Get authorized chat ID from context."""
    return context.bot_data.get("chat_id", "")


def _escape(text: Any) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text))


def _utc_now_str() -> str:
    """Current UTC time formatted for display."""
    return get_utc_now().strftime("%H:%M:%S UTC")


async def _reply(update, text: str, parse_mode: str = "HTML") -> None:
    """Send reply with error handling."""
    if not update or not update.message:
        return
    try:
        # Chunk if too long
        if len(text) > MAX_MESSAGE_LENGTH:
            chunks = _chunk_text(text, MAX_MESSAGE_LENGTH)
            for chunk in chunks:
                await update.message.reply_text(
                    chunk, parse_mode=parse_mode
                )
        else:
            await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Reply failed: {e}")
        try:
            await update.message.reply_text(
                f"❌ Error sending response: {e}"
            )
        except Exception:
            pass


def _chunk_text(text: str, max_len: int) -> List[str]:
    """Split text into chunks at newlines."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        split_pos = remaining[:max_len].rfind("\n")
        if split_pos < max_len // 2:
            split_pos = max_len

        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:].lstrip("\n")

    return chunks


def _format_pnl(value: float) -> str:
    """Format PnL with color emoji."""
    emoji = "📈" if value >= 0 else "📉"
    return f"{emoji} <code>${value:+,.2f}</code>"


def _format_percent(value: float) -> str:
    """Format percentage with sign."""
    return f"{value:+.2f}%"


def _get_auth_role(context) -> str:
    """Get current user's auth role from context."""
    return context.bot_data.get("_auth_role", "unknown")


# ═════════════════════════════════════════════════════════════════
#  BASIC COMMANDS
# ═════════════════════════════════════════════════════════════════

@require_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    role = _get_auth_role(context)
    role_text = f"Role: {role}" if role != "unknown" else ""

    text = (
        f"🤖 <b>4-Brain Trading Bot</b>\n"
        f"{SEPARATOR}\n"
        f"Welcome! I'm your autonomous trading assistant.\n\n"
        f"Use /help to see available commands.\n"
        f"Use /status to check bot status.\n"
        f"\n{role_text}"
    )
    await _reply(update, text)


@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands based on user role."""
    role = _get_auth_role(context)
    is_admin = role == "admin"

    text = (
        f"📋 <b>Available Commands</b>\n"
        f"{SEPARATOR}\n"
        f"\n"
        f"<b>📊 Information:</b>\n"
        f"/status - Bot status overview\n"
        f"/balance - Current balance & PnL\n"
        f"/positions - Open positions\n"
        f"/trades - Recent trade history\n"
        f"/performance - Session metrics\n"
        f"/risk - Risk management report\n"
        f"/limits - Trade limiter status\n"
        f"/market - Market snapshot\n"
        f"/scheduler - Scheduler status\n"
        f"/logs - Recent log entries\n"
        f"/alerts - Pending alerts\n"
    )

    if is_admin:
        text += (
            f"\n"
            f"<b>⚙️ Controls:</b> (Admin)\n"
            f"/pause - Pause trading\n"
            f"/resume - Resume trading\n"
            f"/kill [reason] - Emergency stop\n"
            f"/config [key] [value] - View/set config\n"
            f"\n"
            f"<b>🔓 Risk Management:</b> (Admin)\n"
            f"/unlock - Unlock all risk locks\n"
            f"/reset_risk - Accept losses & restart\n"
            f"/set_risk [param] [value] - Adjust risk\n"
            f"\n"
            f"<b>⚡ Actions:</b> (Admin)\n"
            f"/force_exit - Close all positions\n"
            f"/force_cycle - Trigger trading cycle\n"
            f"/close [symbol] - Close specific position\n"
            f"/buy [symbol] [amount] - Manual buy\n"
            f"/sell [symbol] [amount] - Manual sell\n"
            f"\n"
            f"<b>🔒 Security:</b> (Admin)\n"
            f"/security - Auth stats & audit log\n"
        )
    else:
        text += (
            f"\n"
            f"<i>Admin commands hidden (viewer role)</i>\n"
        )

    text += (
        f"\n"
        f"<b>ℹ️ Other:</b>\n"
        f"/start - Welcome message\n"
        f"/help - This help message\n"
        f"{SEPARATOR}"
    )
    await _reply(update, text)


# ═════════════════════════════════════════════════════════════════
#  INFORMATION COMMANDS
# ═════════════════════════════════════════════════════════════════

@require_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get comprehensive bot status."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        status = controller.get_status()
        scheduler = getattr(controller, "scheduler", None)
        sched_state = scheduler.state.value if scheduler else "unknown"

        running = status.get("running", False)
        status_emoji = "🟢" if running else "🔴"
        status_text = "RUNNING" if running else "STOPPED"

        kill_active = status.get("kill_switch_active", False)
        kill_status = "🚨 ACTIVE" if kill_active else "✅ OFF"

        pnl = status.get("daily_pnl", 0)
        balance = status.get("balance", 0)
        open_pos = status.get("open_positions", 0)

        emergency_ack = status.get("emergency_acknowledged", False)
        emergency_text = "✅ Ack'd" if emergency_ack else "—"

        # Uptime
        uptime = get_uptime_str()

        text = (
            f"{status_emoji} <b>BOT STATUS: {status_text}</b>\n"
            f"{SEPARATOR}\n"
            f"📊 Mode: <code>{_escape(status.get('mode', 'N/A'))}</code>\n"
            f"💰 Balance: <code>${balance:,.2f}</code>\n"
            f"{_format_pnl(pnl)} Daily PnL\n"
            f"📂 Open Positions: <code>{open_pos}</code>\n"
            f"📈 Trades Today: <code>{status.get('trades_today', 0)}</code>\n"
            f"🏆 Win Streak: <code>{status.get('win_streak', 0)}</code>\n"
            f"💔 Loss Streak: <code>{status.get('loss_streak', 0)}</code>\n"
            f"{SEPARATOR}\n"
            f"📋 Scheduler: <code>{sched_state}</code>\n"
            f"🛑 Kill Switch: {kill_status}\n"
            f"🚨 Emergency: {emergency_text}\n"
            f"⏱️ Uptime: <code>{uptime}</code>\n"
            f"🕐 <code>{_utc_now_str()}</code>\n"
            f"{SEPARATOR}"
        )

        if kill_active or not running:
            text += (
                f"\n\n💡 <b>Bot is blocked!</b>\n"
                f"Use /unlock to clear all locks\n"
                f"Use /reset_risk to accept losses & restart"
            )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Status command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check current balance and PnL breakdown."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        state = controller.state
        balance = state.get("balance", 0)
        initial = state.get("initial_balance", 0)
        daily_pnl = state.get("daily_pnl", 0)
        peak = state.get("peak_balance", balance)

        total_pnl = balance - initial if initial else 0
        total_pct = (total_pnl / initial * 100) if initial > 0 else 0
        drawdown = peak - balance if peak > balance else 0
        drawdown_pct = (drawdown / peak * 100) if peak > 0 else 0

        text = (
            f"💰 <b>BALANCE REPORT</b>\n"
            f"{SEPARATOR}\n"
            f"💵 Current: <code>${balance:,.2f}</code>\n"
            f"🏦 Initial: <code>${initial:,.2f}</code>\n"
            f"📊 Total PnL: <code>${total_pnl:+,.2f} "
            f"({_format_percent(total_pct)})</code>\n"
            f"{_format_pnl(daily_pnl)} Today\n"
            f"{SEPARATOR}\n"
            f"📈 Peak: <code>${peak:,.2f}</code>\n"
            f"📉 Drawdown: <code>${drawdown:,.2f} "
            f"({_format_percent(-drawdown_pct)})</code>\n"
            f"{SEPARATOR}"
        )
        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Balance command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List open positions with live PnL."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        positions = controller.state.get_all_positions()

        if not positions:
            await _reply(update, "📂 No open positions")
            return

        lines = [f"📂 <b>OPEN POSITIONS ({len(positions)})</b>", SEPARATOR]

        total_unrealized = 0

        for symbol, pos in positions.items():
            entry = pos.get("entry_price", 0)
            qty = pos.get("quantity", 0)
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")
            side = pos.get("action", "BUY").upper()
            side_emoji = "🟢" if side == "BUY" else "🔴"

            # Try to get current price for unrealized PnL
            current_price = pos.get("current_price", entry)
            if side == "BUY":
                unrealized = (current_price - entry) * qty
            else:
                unrealized = (entry - current_price) * qty

            total_unrealized += unrealized
            pnl_emoji = "📈" if unrealized >= 0 else "📉"

            opened_at = pos.get("opened_at", "")
            duration = ""
            if opened_at:
                duration = f" | {time_ago(opened_at)}"

            lines.append(
                f"\n{side_emoji} <b>{_escape(symbol)}</b>{duration}\n"
                f"  📈 Side: <code>{side}</code> | "
                f"Qty: <code>{qty:.6f}</code>\n"
                f"  💵 Entry: <code>${entry:,.4f}</code>\n"
                f"  {pnl_emoji} Unrealized: "
                f"<code>${unrealized:+,.2f}</code>\n"
            )

            if sl:
                lines.append(f"  🛑 SL: <code>${sl:,.4f}</code>\n")
            if tp:
                lines.append(f"  🎯 TP: <code>${tp:,.4f}</code>")

        lines.append(SEPARATOR)
        pnl_emoji = "📈" if total_unrealized >= 0 else "📉"
        lines.append(
            f"{pnl_emoji} Total Unrealized: "
            f"<code>${total_unrealized:+,.2f}</code>"
        )

        await _reply(update, "\n".join(lines))

    except Exception as e:
        logger.exception(f"Positions command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent trade history."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        history = controller.state.get("trade_history") or []
        recent = history[-10:]  # Last 10 trades

        if not recent:
            await _reply(update, "📜 No trade history")
            return

        lines = [f"📜 <b>RECENT TRADES ({len(recent)})</b>", SEPARATOR]

        total_pnl = 0
        wins = 0

        for trade in reversed(recent):
            symbol = trade.get("symbol", "?")
            pnl = trade.get("pnl_amount", trade.get("pnl", 0))
            pnl_pct = trade.get("pnl_pct", 0)
            pnl_emoji = "✅" if pnl >= 0 else "❌"
            closed_at = trade.get("closed_at", trade.get("timestamp", ""))

            if pnl >= 0:
                wins += 1
            total_pnl += pnl

            time_str = ""
            if closed_at:
                time_str = time_ago(closed_at)

            lines.append(
                f"\n{pnl_emoji} <b>{_escape(symbol)}</b>\n"
                f"  💰 PnL: <code>${pnl:+,.4f}</code> "
                f"({_format_percent(pnl_pct)})\n"
                f"  🕐 {time_str}"
            )

        lines.append(SEPARATOR)
        win_rate = (wins / len(recent) * 100) if recent else 0
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        lines.append(
            f"{pnl_emoji} Total: <code>${total_pnl:+,.2f}</code> | "
            f"Win Rate: <code>{win_rate:.1f}%</code>"
        )

        await _reply(update, "\n".join(lines))

    except Exception as e:
        logger.exception(f"Trades command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show session performance metrics."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        state = controller.state

        # Basic metrics
        balance = state.get("balance", 0)
        initial = state.get("initial_balance", 0)
        trades_today = state.get("trades_today", 0)
        daily_pnl = state.get("daily_pnl", 0)

        # History analysis
        history = state.get("trade_history") or []
        total_trades = len(history)
        wins = sum(1 for t in history if t.get("pnl", 0) >= 0)
        losses = total_trades - wins
        win_rate = (wins / total_trades * 100) if total_trades else 0

        total_pnl = sum(t.get("pnl", 0) for t in history)
        avg_win = 0
        avg_loss = 0
        if wins:
            avg_win = sum(
                t.get("pnl", 0) for t in history if t.get("pnl", 0) >= 0
            ) / wins
        if losses:
            avg_loss = abs(sum(
                t.get("pnl", 0) for t in history if t.get("pnl", 0) < 0
            )) / losses

        profit_factor = (avg_win * wins) / (avg_loss * losses) if losses and avg_loss else 0

        # Streaks
        win_streak = state.get("win_streak", 0)
        loss_streak = state.get("loss_streak", 0)

        text = (
            f"📊 <b>PERFORMANCE METRICS</b>\n"
            f"{SEPARATOR}\n"
            f"💰 Balance: <code>${balance:,.2f}</code>\n"
            f"🏦 Initial: <code>${initial:,.2f}</code>\n"
            f"{_format_pnl(total_pnl)} Total PnL\n"
            f"{_format_pnl(daily_pnl)} Today\n"
            f"{SEPARATOR}\n"
            f"📈 Total Trades: <code>{total_trades}</code>\n"
            f"✅ Wins: <code>{wins}</code> | "
            f"❌ Losses: <code>{losses}</code>\n"
            f"🎯 Win Rate: <code>{win_rate:.1f}%</code>\n"
            f"📈 Avg Win: <code>${avg_win:,.4f}</code>\n"
            f"📉 Avg Loss: <code>${avg_loss:,.4f}</code>\n"
            f"📊 Profit Factor: <code>{profit_factor:.2f}</code>\n"
            f"{SEPARATOR}\n"
            f"🏆 Win Streak: <code>{win_streak}</code>\n"
            f"💔 Loss Streak: <code>{loss_streak}</code>\n"
            f"📈 Today: <code>{trades_today}</code> trades\n"
            f"⏱️ Uptime: <code>{get_uptime_str()}</code>\n"
            f"{SEPARATOR}"
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Performance command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show risk management report."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        ar = getattr(controller, "adaptive_risk", None)
        lg = getattr(controller, "loss_guard", None)

        report = ar.get_risk_report() if ar else {}
        lg_status = lg.get_status() if lg else {}

        can_trade = lg_status.get("can_trade", True)
        can_trade_text = "✅ YES" if can_trade else "❌ NO"
        block = lg_status.get("block_reason", "")
        emergency_ack = lg_status.get("emergency_acknowledged", False)
        emergency_text = "✅" if emergency_ack else "❌"

        text = (
            f"🎯 <b>RISK MANAGEMENT REPORT</b>\n"
            f"{SEPARATOR}\n"
            f"🔓 Can Trade: {can_trade_text}\n"
        )

        if block:
            text += f"⛔ Blocked: <code>{_escape(block)}</code>\n"

        text += (
            f"🚨 Emergency Ack: {emergency_text}\n"
            f"{SEPARATOR}\n"
            f"📊 Current Risk: "
            f"<code>{report.get('current_risk_pct', 0):.3f}%</code>\n"
            f"📏 Base Risk: "
            f"<code>{report.get('base_risk_pct', 0):.2f}%</code>\n"
            f"📈 Win Rate: "
            f"<code>{report.get('win_rate_pct', 0):.1f}%</code>\n"
            f"📊 Total Trades: "
            f"<code>{report.get('total_trades', 0)}</code>\n"
            f"🏆 Win Streak: "
            f"<code>{report.get('win_streak', 0)}</code>\n"
            f"💔 Loss Streak: "
            f"<code>{report.get('loss_streak', 0)}</code>\n"
            f"📉 Drawdown: "
            f"<code>{report.get('drawdown_pct', 0):.2f}%</code>\n"
            f"📐 Kelly (capped): "
            f"<code>{report.get('kelly_capped_pct', 0):.3f}%</code>\n"
            f"📈 Avg Win: "
            f"<code>${report.get('avg_win', 0):.4f}</code>\n"
            f"📉 Avg Loss: "
            f"<code>${report.get('avg_loss', 0):.4f}</code>\n"
            f"{SEPARATOR}\n"
            f"💰 Baseline: "
            f"<code>${lg_status.get('initial_balance', 0):,.2f}</code>\n"
            f"📊 Daily DD: "
            f"<code>{lg_status.get('daily_drawdown_pct', 0):.2f}%</code> "
            f"(max {lg_status.get('daily_limit_pct', 5):.0f}%)\n"
            f"📊 Total DD: "
            f"<code>{lg_status.get('total_drawdown_pct', 0):.2f}%</code> "
            f"(emergency {lg_status.get('emergency_limit_pct', 15):.0f}%)\n"
            f"{SEPARATOR}"
        )

        if not can_trade:
            text += (
                f"\n\n💡 <b>Trading is blocked!</b>\n"
                f"/unlock — Clear all locks\n"
                f"/reset_risk — Accept losses & restart"
            )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Risk command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trade limiter status."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        tl = getattr(controller, "trade_limiter", None)
        if not tl:
            await _reply(update, "❌ Trade limiter not available")
            return

        status = tl.get_status()

        can_trade = status.get("can_trade", True)
        can_trade_text = "✅ YES" if can_trade else "❌ NO"
        cooldown = status.get("rapid_cooldown_active", False)
        cooldown_text = "🔴 ACTIVE" if cooldown else "🟢 OFF"

        text = (
            f"📊 <b>TRADE LIMITER STATUS</b>\n"
            f"{SEPARATOR}\n"
            f"🔓 Can Trade: {can_trade_text}\n"
            f"📈 Today: <code>{status.get('trades_today', 0)}/"
            f"{status.get('max_trades_per_day', 0)}</code>\n"
            f"⏰ This Hour: <code>{status.get('trades_this_hour', 0)}/"
            f"{status.get('max_trades_per_hour', 0)}</code>\n"
            f"⏳ Cooldown: {cooldown_text}\n"
            f"🕐 Min Interval: "
            f"<code>{status.get('min_trade_interval_sec', 0)}s</code>\n"
            f"{SEPARATOR}"
        )
        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Limits command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show market snapshot for all monitored coins."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        # FIXED: Use market_states from controller instead of calling get_snapshot()
        market_states = getattr(controller, "market_states", {})
        
        if not market_states:
            await _reply(update, "📊 No market data available yet\n\nWait for first trading cycle to complete.")
            return

        lines = [f"📊 <b>MARKET SNAPSHOT</b>", SEPARATOR]

        for symbol, market in market_states.items():
            try:
                price = market.price
                trend = market.trend
                rsi = market.rsi if market.rsi else 0
                regime = getattr(market, 'regime', 'unknown') or 'unknown'
                volatility = getattr(market, 'volatility_regime', 'unknown') or 'unknown'

                # Get AI prediction if available
                ai_prediction = getattr(market, 'ai_prediction', None)
                ai_signal = "N/A"
                ai_conf = 0
                if ai_prediction:
                    ai_signal = ai_prediction.get("signal", "N/A")
                    ai_conf = ai_prediction.get("confidence", 0)

                trend_emoji = {
                    "bullish": "📈", "bearish": "📉", "sideways": "➡️"
                }.get(trend.lower(), "📊")

                lines.append(
                    f"\n{trend_emoji} <b>{_escape(symbol)}</b>\n"
                    f"  💵 Price: <code>${price:,.4f}</code>\n"
                    f"  📈 Trend: <code>{trend.capitalize()}</code>\n"
                    f"  📐 RSI: <code>{rsi:.1f}</code>\n"
                    f"  🔄 Regime: <code>{regime.capitalize()}</code>\n"
                    f"  🌪️ Vol: <code>{volatility.capitalize()}</code>\n"
                    f"  🤖 AI: <code>{ai_signal} ({ai_conf}%)</code>"
                )
            except Exception as ex:
                lines.append(
                    f"\n❌ <b>{_escape(symbol)}</b>: Error - {_escape(ex)}"
                )

        lines.append(SEPARATOR)
        lines.append(f"🕐 <code>{_utc_now_str()}</code>")

        await _reply(update, "\n".join(lines))

    except Exception as e:
        logger.exception(f"Market command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_scheduler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show scheduler status and metrics."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        scheduler = getattr(controller, "scheduler", None)
        if not scheduler:
            await _reply(update, "❌ Scheduler not available")
            return

        stats = scheduler.get_stats()
        state_emoji = {
            "running": "🟢",
            "paused": "🟡",
            "market_closed": "🌙",
            "error_backoff": "🟠",
            "stopped": "🔴",
        }.get(stats.get("state", ""), "⚪")

        text = (
            f"📋 <b>SCHEDULER STATUS</b>\n"
            f"{SEPARATOR}\n"
            f"{state_emoji} State: "
            f"<code>{stats.get('state', 'unknown')}</code>\n"
            f"⏱️ Interval: "
            f"<code>{stats.get('interval_seconds', 0)}s</code>\n"
            f"🔁 Total Cycles: "
            f"<code>{stats.get('total_cycles', 0)}</code>\n"
            f"❌ Total Errors: "
            f"<code>{stats.get('total_errors', 0)}</code>\n"
            f"📊 Error Rate: "
            f"<code>{stats.get('error_rate_pct', 0):.1f}%</code>\n"
            f"⚡ Consecutive Errors: "
            f"<code>{stats.get('consecutive_errors', 0)}</code>\n"
            f"{SEPARATOR}\n"
            f"⏱️ Avg Latency: "
            f"<code>{stats.get('avg_latency_sec', 0):.3f}s</code>\n"
            f"📈 Peak Latency: "
            f"<code>{stats.get('peak_latency_sec', 0):.3f}s</code>\n"
            f"🏪 Market Aware: "
            f"<code>{stats.get('market_aware', False)}</code>\n"
            f"⏱️ Uptime: "
            f"<code>{stats.get('uptime_human', 'N/A')}</code>\n"
            f"{SEPARATOR}\n"
            f"📊 Recent Window: "
            f"<code>{stats.get('recent_window_size', 0)}</code> cycles\n"
            f"✅ Recent Success: "
            f"<code>{stats.get('recent_success_rate_pct', 100):.1f}%</code>\n"
            f"📈 Recent Trades: "
            f"<code>{stats.get('recent_trades_executed', 0)}</code>\n"
            f"{SEPARATOR}"
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Scheduler command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent log entries."""
    try:
        args = context.args or []
        level = args[0].upper() if args else None
        count = int(args[1]) if len(args) > 1 else 15

        logs = get_recent_logs(level=level, last_n=count)

        if not logs:
            await _reply(update, "📜 No log entries found")
            return

        lines = [f"📜 <b>RECENT LOGS ({len(logs)})</b>", SEPARATOR]

        level_emoji = {
            "DEBUG": "🔍",
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "CRITICAL": "🚨",
        }

        for log in logs[-15:]:  # Limit display
            emoji = level_emoji.get(log.get("level", ""), "📝")
            time = log.get("ts", "")[:8] if log.get("ts") else ""
            msg = log.get("message", "")[:80]

            lines.append(
                f"\n{emoji} <code>{time}</code>\n"
                f"  {_escape(msg)}"
            )

        lines.append(SEPARATOR)
        lines.append(f"💡 Usage: /logs [level] [count]")

        await _reply(update, "\n".join(lines))

    except Exception as e:
        logger.exception(f"Logs command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_auth
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending system alerts."""
    try:
        alerts = get_pending_alerts()

        if not alerts:
            await _reply(
                update,
                "✅ No pending alerts\n\n"
                "System is operating normally."
            )
            return

        lines = [f"🚨 <b>PENDING ALERTS ({len(alerts)})</b>", SEPARATOR]

        for alert in alerts[:10]:
            level = alert.get("level", "ERROR")
            emoji = "🚨" if level == "CRITICAL" else "⚠️"
            time = alert.get("ts", "")[:19]
            msg = alert.get("message", "")[:100]
            logger_name = alert.get("logger", "unknown")

            lines.append(
                f"\n{emoji} <b>{level}</b> "
                f"<code>{time}</code>\n"
                f"  📍 {_escape(logger_name)}\n"
                f"  {_escape(msg)}"
            )

        if len(alerts) > 10:
            lines.append(f"\n... +{len(alerts) - 10} more alerts")

        lines.append(SEPARATOR)

        await _reply(update, "\n".join(lines))

    except Exception as e:
        logger.exception(f"Alerts command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


# ═════════════════════════════════════════════════════════════════
#  CONTROL COMMANDS (Admin)
# ═════════════════════════════════════════════════════════════════

@require_admin
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
            f"⏸️ Trading <b>PAUSED</b>\n\n"
            f"Use /resume to continue."
        )
    except Exception as e:
        logger.exception(f"Pause command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume trading and unlock all locks."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        ks = getattr(controller, "kill_switch", None)
        was_kill_active = ks.is_active() if ks else False
        was_bot_active = controller.state.get("bot_active", False)

        controller.resume()

        changes = []
        if was_kill_active:
            changes.append("🛑 Kill switch deactivated")
        if not was_bot_active:
            changes.append("▶️ Bot reactivated")
        if not changes:
            changes.append("ℹ️ Bot was already running")

        balance = controller.state.get("balance", 0)
        initial = controller.state.get("initial_balance", 0)

        text = (
            f"▶️ <b>TRADING RESUMED</b>\n"
            f"{SEPARATOR}\n"
            f"{chr(10).join(changes)}\n"
            f"{SEPARATOR}\n"
            f"💰 Balance: <code>${balance:,.2f}</code>\n"
            f"🏦 Baseline: <code>${initial:,.2f}</code>\n"
            f"{SEPARATOR}\n"
            f"✅ Bot is now monitoring!\n\n"
            f"💡 If bot re-locks, use /reset_risk\n"
            f"to accept past losses and start fresh."
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Resume command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate kill switch."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        args = context.args or []
        reason = " ".join(args) if args else "Manual Telegram trigger"

        ks = getattr(controller, "kill_switch", None)
        if ks:
            ks.activate(
                reason=reason,
                source="telegram",
                auto_resume_minutes=None,
            )

        await _reply(
            update,
            f"🚨 <b>KILL SWITCH ACTIVATED</b>\n\n"
            f"Reason: {_escape(reason)}\n\n"
            f"All trading has been halted.\n"
            f"Use /unlock to clear all locks.\n"
            f"Use /resume to restart trading."
        )
    except Exception as e:
        logger.exception(f"Kill command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View or update configuration."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        args = context.args or []

        if not args:
            # Show current config
            state = controller.state
            mode = state.get("mode", "PAPER")
            interval = getattr(controller, "interval", 300)
            coins = state.get("coins", [])

            scheduler = getattr(controller, "scheduler", None)
            sched_interval = scheduler.config.interval if scheduler else interval
            market_aware = scheduler.config.market_aware if scheduler else False

            text = (
                f"⚙️ <b>CONFIGURATION</b>\n"
                f"{SEPARATOR}\n"
                f"📊 Mode: <code>{mode}</code>\n"
                f"⏱️ Interval: <code>{sched_interval}s</code>\n"
                f"🪙 Coins: <code>{', '.join(coins) if coins else 'None'}</code>\n"
                f"🏪 Market Aware: <code>{market_aware}</code>\n"
                f"{SEPARATOR}\n"
                f"💡 Usage: /config [key] [value]\n"
                f"Example: /config interval 60"
            )
            await _reply(update, text)
            return

        key = args[0].lower()
        value = args[1] if len(args) > 1 else None

        if value is None:
            await _reply(
                update,
                f"❌ Missing value for {key}\n"
                f"Usage: /config {key} [value]"
            )
            return

        # Handle specific config keys
        scheduler = getattr(controller, "scheduler", None)

        if key == "interval":
            new_interval = int(value)
            if scheduler:
                scheduler.update_interval(new_interval)
            await _reply(
                update,
                f"✅ Interval updated to <code>{new_interval}s</code>"
            )

        elif key == "market_aware":
            enabled = value.lower() in ("true", "1", "yes", "on")
            if scheduler:
                scheduler.set_market_aware(enabled)
            await _reply(
                update,
                f"✅ Market awareness: "
                f"<code>{'ON' if enabled else 'OFF'}</code>"
            )

        else:
            await _reply(
                update,
                f"❌ Unknown config key: {_escape(key)}\n"
                f"Available: interval, market_aware"
            )

    except Exception as e:
        logger.exception(f"Config command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


# ═════════════════════════════════════════════════════════════════
#  RISK COMMANDS (Admin)
# ═════════════════════════════════════════════════════════════════

@require_admin
async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full risk system unlock."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        unlock_fn = getattr(controller, "unlock_risk", None)
        if unlock_fn:
            unlock_fn(source="telegram")

        balance = controller.state.get("balance", 0)
        initial = controller.state.get("initial_balance", 0)

        lg = getattr(controller, "loss_guard", None)
        emergency_pct = lg.emergency_drawdown_pct if lg else 0.15
        emergency_threshold = initial * (1 - emergency_pct) if initial else 0

        text = (
            f"🔓 <b>RISK UNLOCKED</b>\n"
            f"{SEPARATOR}\n"
            f"✅ Kill switch: OFF\n"
            f"✅ Cooldown: CLEARED\n"
            f"✅ Loss streak: RESET\n"
            f"✅ Emergency: ACKNOWLEDGED\n"
            f"✅ Bot: ACTIVE\n"
            f"{SEPARATOR}\n"
            f"💰 Balance: <code>${balance:,.2f}</code>\n"
            f"🏦 Baseline: <code>${initial:,.2f}</code>\n"
            f"🚨 Emergency at: <code>${emergency_threshold:,.2f}</code>\n"
            f"{SEPARATOR}\n"
            f"⚠️ Emergency won't re-trigger until\n"
            f"baseline is reset via /reset_risk\n\n"
            f"💡 If baseline is wrong (old value),\n"
            f"use /reset_risk to fix it."
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Unlock command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_reset_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset risk baseline — accept all past losses and start fresh."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        reset_fn = getattr(controller, "reset_risk_baseline", None)
        if not reset_fn:
            await _reply(update, "❌ Reset function not available")
            return

        result = reset_fn(source="telegram")

        if not result.get("reset"):
            await _reply(
                update,
                "❌ Cannot reset — balance is zero"
            )
            return

        new_baseline = result.get("new_initial", 0)
        old_initial = result.get("old_initial", 0)
        emergency_threshold = result.get("emergency_threshold", 0)

        text = (
            f"🔄 <b>RISK BASELINE RESET</b>\n"
            f"{SEPARATOR}\n"
            f"📉 Old baseline: <code>${old_initial:,.2f}</code>\n"
            f"📈 New baseline: <code>${new_baseline:,.2f}</code>\n"
            f"{SEPARATOR}\n"
            f"✅ All locks: CLEARED\n"
            f"✅ Streaks: RESET\n"
            f"✅ Daily PnL: RESET\n"
            f"✅ Drawdown: 0%\n"
            f"✅ Bot: ACTIVE\n"
            f"{SEPARATOR}\n"
            f"🚨 Emergency now triggers at:\n"
            f"   <code>${emergency_threshold:,.2f}</code> "
            f"(15% below ${new_baseline:,.2f})\n"
            f"{SEPARATOR}\n"
            f"✅ Past losses accepted.\n"
            f"🤖 Bot is monitoring with fresh metrics!"
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Reset risk command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_set_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adjust risk parameters."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        args = context.args or []

        ar = getattr(controller, "adaptive_risk", None)
        lg = getattr(controller, "loss_guard", None)

        if not args:
            # Show current settings
            base_risk = ar.base_risk_pct if ar else 0
            max_risk = ar.max_risk_pct if ar else 0
            daily_limit = getattr(lg, 'max_daily_loss_pct', 0) if lg else 0
            emergency = lg.emergency_drawdown_pct if lg else 0

            text = (
                f"⚙️ <b>RISK PARAMETERS</b>\n"
                f"{SEPARATOR}\n"
                f"📏 Base Risk: <code>{base_risk*100:.2f}%</code>\n"
                f"📈 Max Risk: <code>{max_risk*100:.2f}%</code>\n"
                f"📉 Daily Limit: <code>{daily_limit*100:.1f}%</code>\n"
                f"🚨 Emergency: <code>{emergency*100:.1f}%</code>\n"
                f"{SEPARATOR}\n"
                f"💡 Usage: /set_risk [param] [value]\n"
                f"Params: base_risk, max_risk, daily_limit"
            )
            await _reply(update, text)
            return

        param = args[0].lower()
        value = float(args[1]) if len(args) > 1 else None

        if value is None:
            await _reply(
                update,
                f"❌ Missing value\n"
                f"Usage: /set_risk {param} [value]"
            )
            return

        # Apply changes
        if param == "base_risk" and ar:
            ar.base_risk_pct = value / 100
            await _reply(
                update,
                f"✅ Base risk set to <code>{value:.2f}%</code>"
            )
        elif param == "max_risk" and ar:
            ar.max_risk_pct = value / 100
            await _reply(
                update,
                f"✅ Max risk set to <code>{value:.2f}%</code>"
            )
        elif param == "daily_limit" and lg:
            if hasattr(lg, 'max_daily_loss_pct'):
                lg.max_daily_loss_pct = value / 100
                await _reply(
                    update,
                    f"✅ Daily limit set to <code>{value:.1f}%</code>"
                )
            else:
                await _reply(update, "❌ Daily limit parameter not available")
        else:
            await _reply(
                update,
                f"❌ Unknown param: {_escape(param)}\n"
                f"Available: base_risk, max_risk, daily_limit"
            )

    except Exception as e:
        logger.exception(f"Set risk command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


# ═════════════════════════════════════════════════════════════════
#  ACTION COMMANDS (Admin)
# ═════════════════════════════════════════════════════════════════

@require_admin
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

        exit_fn = getattr(controller, "force_exit_all", None)
        if exit_fn:
            exit_fn(reason="Telegram /force_exit")

        await _reply(
            update,
            f"✅ <b>FORCE EXIT COMPLETE</b>\n\n"
            f"Closed {count} position(s)"
        )
    except Exception as e:
        logger.exception(f"Force exit command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_force_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a trading cycle."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        ks = getattr(controller, "kill_switch", None)
        if ks and ks.is_active():
            await _reply(
                update,
                f"🔒 <b>Cannot force cycle — kill switch is active</b>\n\n"
                f"Use /unlock first, then try again."
            )
            return

        scheduler = getattr(controller, "scheduler", None)
        if not scheduler:
            await _reply(update, "❌ Scheduler not available")
            return

        await _reply(update, "⚡ Triggering manual cycle...")

        result = await scheduler.force_cycle()

        if result:
            status = "✅" if result.success else "❌"
            duration = result.duration_seconds
            trades = result.trades_executed

            text = (
                f"{status} <b>Manual cycle completed</b>\n"
                f"⏱️ Duration: <code>{duration:.2f}s</code>\n"
                f"📈 Trades: <code>{trades}</code>"
            )
            if result.error:
                text += f"\n❌ Error: <code>{_escape(result.error)}</code>"

            await _reply(update, text)
        else:
            await _reply(update, "✅ Manual cycle completed")

    except Exception as e:
        logger.exception(f"Force cycle command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close a specific position by symbol."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        args = context.args or []
        if not args:
            await _reply(
                update,
                "❌ Symbol required\n"
                "Usage: /close BTC/USD"
            )
            return

        symbol = args[0].upper()
        positions = controller.state.get_all_positions()

        if symbol not in positions:
            await _reply(
                update,
                f"❌ No position found for {_escape(symbol)}\n"
                f"Open positions: {', '.join(positions.keys()) or 'None'}"
            )
            return

        await _reply(update, f"⏳ Closing {_escape(symbol)}...")

        close_fn = getattr(controller, "close_position", None)
        if close_fn:
            close_fn(symbol, reason="Telegram /close")

        await _reply(
            update,
            f"✅ Position closed: <b>{_escape(symbol)}</b>"
        )

    except Exception as e:
        logger.exception(f"Close command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual buy order."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        args = context.args or []
        if len(args) < 2:
            await _reply(
                update,
                "❌ Symbol and amount required\n"
                "Usage: /buy BTC/USD 100"
            )
            return

        symbol = args[0].upper()
        amount = float(args[1])

        await _reply(
            update,
            f"⏳ Placing BUY order: {_escape(symbol)} ${amount:,.2f}..."
        )

        # Execute via controller
        execute_fn = getattr(controller, "manual_order", None)
        if execute_fn:
            result = execute_fn(symbol, "BUY", amount)
            if result.get("success"):
                await _reply(
                    update,
                    f"✅ BUY order placed\n"
                    f"🪙 {_escape(symbol)}\n"
                    f"💰 ${amount:,.2f}"
                )
            else:
                await _reply(
                    update,
                    f"❌ Order failed: {result.get('error', 'Unknown')}"
                )
        else:
            await _reply(update, "❌ Manual orders not supported")

    except Exception as e:
        logger.exception(f"Buy command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


@require_admin
async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual sell order."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        args = context.args or []
        if len(args) < 2:
            await _reply(
                update,
                "❌ Symbol and amount required\n"
                "Usage: /sell BTC/USD 100"
            )
            return

        symbol = args[0].upper()
        amount = float(args[1])

        await _reply(
            update,
            f"⏳ Placing SELL order: {_escape(symbol)} ${amount:,.2f}..."
        )

        execute_fn = getattr(controller, "manual_order", None)
        if execute_fn:
            result = execute_fn(symbol, "SELL", amount)
            if result.get("success"):
                await _reply(
                    update,
                    f"✅ SELL order placed\n"
                    f"🪙 {_escape(symbol)}\n"
                    f"💰 ${amount:,.2f}"
                )
            else:
                await _reply(
                    update,
                    f"❌ Order failed: {result.get('error', 'Unknown')}"
                )
        else:
            await _reply(update, "❌ Manual orders not supported")

    except Exception as e:
        logger.exception(f"Sell command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")


# ═════════════════════════════════════════════════════════════════
#  SECURITY COMMANDS (Admin)
# ═════════════════════════════════════════════════════════════════

@require_admin
async def cmd_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show authentication stats and audit log."""
    try:
        stats = get_security_stats()
        audit = get_audit_log(last_n=5)

        text = (
            f"🔒 <b>SECURITY STATS</b>\n"
            f"{SEPARATOR}\n"
            f"📊 Total Attempts: <code>{stats.get('total_attempts', 0)}</code>\n"
            f"✅ Granted: <code>{stats.get('granted', 0)}</code>\n"
            f"❌ Denied: <code>{stats.get('denied', 0)}</code>\n"
            f"🚫 Rate Limited: <code>{stats.get('rate_limited', 0)}</code>\n"
            f"👤 Unique Unauth: "
            f"<code>{stats.get('unique_unauthorized_chats', 0)}</code>\n"
            f"{SEPARATOR}\n"
            f"👑 Admin IDs: "
            f"<code>{stats.get('admin_ids_configured', 0)}</code>\n"
            f"👁️ Viewer IDs: "
            f"<code>{stats.get('viewer_ids_configured', 0)}</code>\n"
            f"🔑 Active Sessions: "
            f"<code>{len(stats.get('active_temp_sessions', {}))}</code>\n"
            f"{SEPARATOR}\n"
        )

        if audit:
            text += "<b>Recent Auth Attempts:</b>\n"
            for entry in audit:
                emoji = "✅" if entry.get("result") == "granted" else "❌"
                text += (
                    f"{emoji} {entry.get('time', '')} | "
                    f"{entry.get('handler', '')} | "
                    f"{entry.get('result', '')}\n"
                )
        else:
            text += "<i>No recent attempts</i>\n"

        text += SEPARATOR

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Security command error: {e}")
        await _reply(update, f"❌ Error: {_escape(e)}")