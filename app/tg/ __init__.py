# app/tg/__init__.py

"""
Telegram Integration Module — Production Grade v2

This module provides complete Telegram bot integration for the trading bot:

Components:
───────────
• TelegramNotifier  - Async notification sender with retry, rate limiting
• TelegramAuth      - Role-based authentication (@require_auth, @require_admin)
• TelegramCommands  - 27 command handlers for bot control

Architecture:
─────────────
    ┌────────────────────────────────────────────────────────────┐
    │                    Telegram Bot                            │
    │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
    │  │   Notifier   │  │     Auth     │  │    Commands      │ │
    │  │              │  │              │  │                  │ │
    │  │ • Lifecycle  │  │ • @require_  │  │ • /status        │ │
    │  │ • Trades     │  │   auth       │  │ • /balance       │ │
    │  │ • Alerts     │  │ • @require_  │  │ • /positions     │ │
    │  │ • Reports    │  │   admin      │  │ • /trades        │ │
    │  │ • Errors     │  │ • Role-based │  │ • /risk          │ │
    │  │ • Custom     │  │ • Rate limit │  │ • /pause/resume  │ │
    │  │              │  │ • Audit log  │  │ • /kill/unlock   │ │
    │  └──────────────┘  └──────────────┘  │ • /force_cycle   │ │
    │                                       │ • ... (27 total) │ │
    │                                       └──────────────────┘ │
    └────────────────────────────────────────────────────────────┘

Usage:
──────
    # Start the bot
    from app.tg import start_telegram_bot, stop_telegram_bot

    app = await start_telegram_bot(controller, drop_pending_updates=True)

    # On shutdown
    await stop_telegram_bot(app)

    # Use notifier directly
    from app.tg import TelegramNotifier

    notifier = TelegramNotifier(bot, chat_id)
    await notifier.send_trade_executed(...)

    # Use auth decorators
    from app.tg import require_auth, require_admin

    @require_auth
    async def cmd_status(update, context):
        ...

    @require_admin
    async def cmd_kill(update, context):
        ...

Environment Variables:
──────────────────────
    TELEGRAM_BOT_TOKEN      - Bot token from @BotFather
    TELEGRAM_CHAT_ID        - Admin chat ID(s), comma-separated
    TELEGRAM_VIEWER_IDS     - Viewer chat ID(s), comma-separated (optional)
    TELEGRAM_AUTH_RATE_LIMIT - Max unauthorized attempts per minute (default: 5)
    TELEGRAM_AUTH_COOLDOWN  - Reply cooldown in seconds (default: 30)

Features:
─────────
• Role-based access control (admin vs viewer)
• Message rate limiting (respects Telegram API limits)
• Automatic message chunking (>4096 chars)
• Retry with exponential backoff
• HTML escaping for security
• Audit trail for all auth attempts
• Temporary elevated sessions
• Proactive alert dispatch from logger buffer
• Graceful degradation when telegram library unavailable
"""

from typing import Any, Dict, List, Optional, Set

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  CHECK TELEGRAM AVAILABILITY
# ═════════════════════════════════════════════════════════════════

try:
    from telegram import Update
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning(
        "python-telegram-bot not installed — "
        "Telegram features disabled. "
        "Install with: pip install python-telegram-bot>=20.0"
    )

# ═════════════════════════════════════════════════════════════════
#  PUBLIC IMPORTS
# ═════════════════════════════════════════════════════════════════

# Bot lifecycle
from app.tg.bot import (
    start_telegram_bot,
    stop_telegram_bot,
    TelegramNotifier,
    TELEGRAM_AVAILABLE as BOT_AVAILABLE,
)

# Authentication
from app.tg.auth import (
    require_auth,
    require_admin,
    auth_optional,
    get_authorized_chat_id,
    get_all_authorized_ids,
    get_chat_id_from_context,
    is_authorized,
    get_role,
    grant_temp_session,
    revoke_temp_session,
    get_active_sessions,
    get_audit_log,
    get_security_stats,
    clear_rate_limits,
    AuthRole,
    AuthResult,
    TELEGRAM_AVAILABLE as AUTH_AVAILABLE,
)

# Commands
from app.tg.commands import (
    setup_commands,
    TELEGRAM_AVAILABLE as COMMANDS_AVAILABLE,
)

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════════

__all__ = [
    # ── Bot Lifecycle ───────────────────────────────────────────
    "start_telegram_bot",
    "stop_telegram_bot",
    "TelegramNotifier",

    # ── Authentication ──────────────────────────────────────────
    "require_auth",
    "require_admin",
    "auth_optional",
    "get_authorized_chat_id",
    "get_all_authorized_ids",
    "get_chat_id_from_context",
    "is_authorized",
    "get_role",
    "AuthRole",
    "AuthResult",

    # ── Session Management ──────────────────────────────────────
    "grant_temp_session",
    "revoke_temp_session",
    "get_active_sessions",

    # ── Audit & Security ────────────────────────────────────────
    "get_audit_log",
    "get_security_stats",
    "clear_rate_limits",

    # ── Commands ────────────────────────────────────────────────
    "setup_commands",

    # ── Status ──────────────────────────────────────────────────
    "TELEGRAM_AVAILABLE",
    "get_telegram_info",
    "is_telegram_configured",
]

__version__ = "2.0.0"


# ═════════════════════════════════════════════════════════════════
#  MODULE INFO
# ═════════════════════════════════════════════════════════════════

def get_telegram_info() -> Dict[str, Any]:
    """
    Get comprehensive information about the Telegram module.

    Returns:
        Dict with version, availability, configuration status
    """
    import os

    token_set = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    chat_id_set = bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())
    viewer_ids_set = bool(os.getenv("TELEGRAM_VIEWER_IDS", "").strip())

    admin_ids = get_all_authorized_ids() - set(
        os.getenv("TELEGRAM_VIEWER_IDS", "").split(",")
    )
    viewer_ids = set(
        p.strip() for p in os.getenv("TELEGRAM_VIEWER_IDS", "").split(",")
        if p.strip()
    )

    return {
        "version": __version__,
        "telegram_library_available": TELEGRAM_AVAILABLE,
        "bot_available": BOT_AVAILABLE,
        "auth_available": AUTH_AVAILABLE,
        "commands_available": COMMANDS_AVAILABLE,
        "configuration": {
            "token_configured": token_set,
            "chat_id_configured": chat_id_set,
            "viewer_ids_configured": viewer_ids_set,
            "admin_count": len(admin_ids),
            "viewer_count": len(viewer_ids),
        },
        "components": {
            "TelegramNotifier": {
                "description": "Async notification sender",
                "features": [
                    "message_chunking",
                    "retry_with_backoff",
                    "rate_limiting",
                    "category_cooldowns",
                    "delivery_tracking",
                    "html_escaping",
                ],
            },
            "TelegramAuth": {
                "description": "Role-based authentication",
                "features": [
                    "admin_role",
                    "viewer_role",
                    "rate_limiting",
                    "reply_cooldown",
                    "audit_trail",
                    "temp_sessions",
                ],
            },
            "TelegramCommands": {
                "description": "Command handlers (27 total)",
                "categories": [
                    "information (13)",
                    "controls (4)",
                    "risk_management (3)",
                    "actions (5)",
                    "security (1)",
                    "basic (2)",
                ],
            },
        },
        "security_stats": get_security_stats() if TELEGRAM_AVAILABLE else {},
    }


def is_telegram_configured() -> bool:
    """
    Check if Telegram is fully configured and ready.

    Returns:
        True if library installed AND token + chat_id configured
    """
    import os

    if not TELEGRAM_AVAILABLE:
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    return bool(token and chat_id)


def get_notifier_stub() -> Optional["TelegramNotifier"]:
    """
    Get a stub notifier that does nothing.

    Useful when Telegram is not configured but code expects a notifier.

    Returns:
        None (caller should check for None)
    """
    return None


# ═════════════════════════════════════════════════════════════════
#  QUICK SETUP HELPER
# ═════════════════════════════════════════════════════════════════

async def create_notifier(
    bot: Any,
    chat_id: Optional[str] = None,
) -> Optional[TelegramNotifier]:
    """
    Create a TelegramNotifier with optional chat_id override.

    Args:
        bot: Telegram Bot instance
        chat_id: Override chat ID (default: from env)

    Returns:
        TelegramNotifier instance or None if not configured

    Usage:
        notifier = await create_notifier(app.bot)
        if notifier:
            await notifier.send_custom("Hello!")
    """
    if not TELEGRAM_AVAILABLE:
        logger.warning("Cannot create notifier — Telegram not available")
        return None

    resolved_chat_id = chat_id or get_authorized_chat_id()

    if not resolved_chat_id:
        logger.warning("Cannot create notifier — no chat ID configured")
        return None

    return TelegramNotifier(bot=bot, chat_id=resolved_chat_id)


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Run module diagnostics.

    Usage:
        python -m app.tg
    """
    print("=" * 64)
    print("  Telegram Module v2 — Diagnostics")
    print("=" * 64)

    info = get_telegram_info()

    print(f"\n  Version: {info['version']}")
    print(f"\n  ── Availability ──")
    print(f"  Library installed:  {'✅' if info['telegram_library_available'] else '❌'}")
    print(f"  Bot module:         {'✅' if info['bot_available'] else '❌'}")
    print(f"  Auth module:        {'✅' if info['auth_available'] else '❌'}")
    print(f"  Commands module:    {'✅' if info['commands_available'] else '❌'}")

    print(f"\n  ── Configuration ──")
    config = info['configuration']
    print(f"  Token configured:   {'✅' if config['token_configured'] else '❌'}")
    print(f"  Chat ID configured: {'✅' if config['chat_id_configured'] else '❌'}")
    print(f"  Admin IDs:          {config['admin_count']}")
    print(f"  Viewer IDs:         {config['viewer_count']}")
    print(f"  Fully configured:   {'✅' if is_telegram_configured() else '❌'}")

    print(f"\n  ── Components ──")
    for name, comp in info['components'].items():
        print(f"\n  📦 {name}: {comp['description']}")
        if 'features' in comp:
            features = ', '.join(comp['features'][:4])
            extra = len(comp['features']) - 4
            print(f"     Features: {features}" + (f" +{extra}" if extra > 0 else ""))
        if 'categories' in comp:
            cats = ', '.join(comp['categories'][:3])
            print(f"     Categories: {cats}")

    print(f"\n  ── Security Stats ──")
    if info['security_stats']:
        stats = info['security_stats']
        print(f"  Total attempts:     {stats.get('total_attempts', 0)}")
        print(f"  Granted:            {stats.get('granted', 0)}")
        print(f"  Denied:             {stats.get('denied', 0)}")
        print(f"  Rate limited:       {stats.get('rate_limited', 0)}")
    else:
        print("  (Telegram not available)")

    print(f"\n  ── Exports ──")
    print(f"  Total symbols: {len(__all__)}")

    print("\n" + "=" * 64)
    print("  ✅ Telegram Module v2 — Ready")
    print("=" * 64)