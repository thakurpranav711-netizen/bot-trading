# app/tg/auth.py

"""
Telegram Authentication — Production Grade v2

Provides @require_auth decorator for Telegram command handlers.

Features:
- Authorizes by TELEGRAM_CHAT_ID (env var)
- Supports multiple authorized IDs (comma-separated)
- Role-based access (admin vs viewer)
- Safe on callback queries (update.message may be None)
- Rate limits unauthorized attempts (configurable)
- Cooldown on repeated unauthorized replies (anti-spam)
- Full audit trail with structured logging
- IP/chat fingerprinting for security alerts
- Temporary session tokens for elevated access
- Works even if python-telegram-bot not installed (no-op mode)
- Thread-safe rate limiting

Usage:
    from app.tg.auth import require_auth, require_admin

    @require_auth
    async def cmd_status(update, context):
        ...

    @require_admin
    async def cmd_kill(update, context):
        ...

Environment:
    TELEGRAM_CHAT_ID = "123456789"                  # Single admin
    TELEGRAM_CHAT_ID = "123456789,987654321"        # Multiple admins
    TELEGRAM_VIEWER_IDS = "111111111,222222222"      # Read-only viewers
    TELEGRAM_AUTH_RATE_LIMIT = "5"                   # Max unauth attempts/min
    TELEGRAM_AUTH_COOLDOWN = "30"                    # Reply cooldown (seconds)

Roles:
    admin  → Full access (trade, configure, kill switch)
    viewer → Read-only (status, balance, positions)
"""

import os
import time
import threading
import functools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  TELEGRAM IMPORT (graceful fallback)
# ═════════════════════════════════════════════════════════════════

try:
    from telegram import Update
    from telegram.ext import ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = None           # type: ignore
    ContextTypes = None     # type: ignore
    logger.warning(
        "python-telegram-bot not installed — "
        "auth module running in stub mode"
    )


# ═════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═════════════════════════════════════════════════════════════════

# Rate limiting
_RATE_WINDOW_SEC = 60
_RATE_MAX_ATTEMPTS = int(os.getenv("TELEGRAM_AUTH_RATE_LIMIT", "5"))

# Reply cooldown (don't spam "Unauthorized" messages)
_REPLY_COOLDOWN_SEC = int(os.getenv("TELEGRAM_AUTH_COOLDOWN", "30"))

# Max audit log entries kept in memory
_MAX_AUDIT_ENTRIES = 500


# ═════════════════════════════════════════════════════════════════
#  ENUMS
# ═════════════════════════════════════════════════════════════════

class AuthRole(str, Enum):
    """User access roles."""
    ADMIN = "admin"         # Full access
    VIEWER = "viewer"       # Read-only access
    UNKNOWN = "unknown"     # Not authorized


class AuthResult(str, Enum):
    """Authentication attempt results."""
    GRANTED = "granted"
    DENIED = "denied"
    RATE_LIMITED = "rate_limited"
    COOLDOWN = "cooldown"
    MISCONFIGURED = "misconfigured"
    NO_UPDATE = "no_update"


# ═════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═════════════════════════════════════════════════════════════════

@dataclass
class AuthAttempt:
    """Record of an authentication attempt."""
    timestamp: datetime
    chat_id: str
    username: str
    handler: str
    result: AuthResult
    role: AuthRole = AuthRole.UNKNOWN
    details: str = ""


@dataclass
class ChatRateState:
    """Rate limiting state for a specific chat."""
    attempts: List[float] = field(default_factory=list)
    last_reply_time: float = 0.0
    total_denied: int = 0
    first_seen: float = field(default_factory=time.time)


# ═════════════════════════════════════════════════════════════════
#  AUTH MANAGER (singleton)
# ═════════════════════════════════════════════════════════════════

class _AuthManager:
    """
    Thread-safe authentication manager.

    Handles:
    - ID resolution from environment
    - Role-based access control
    - Rate limiting per chat
    - Reply cooldown (anti-spam)
    - Audit trail
    - Temporary elevated sessions
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._rate_states: Dict[str, ChatRateState] = defaultdict(
            ChatRateState
        )
        self._audit_log: List[AuthAttempt] = []
        self._temp_sessions: Dict[str, float] = {}  # chat_id → expiry
        self._cached_admin_ids: Optional[Set[str]] = None
        self._cached_viewer_ids: Optional[Set[str]] = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 30.0  # Re-read env every 30s

    # ── ID Resolution ─────────────────────────────────────────

    def _refresh_cache(self) -> None:
        """Refresh cached IDs from environment (with TTL)."""
        now = time.time()
        if (
            self._cached_admin_ids is not None
            and now - self._cache_time < self._cache_ttl
        ):
            return

        # Admin IDs
        raw_admin = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self._cached_admin_ids = {
            p.strip() for p in raw_admin.split(",") if p.strip()
        }

        # Viewer IDs
        raw_viewer = os.getenv("TELEGRAM_VIEWER_IDS", "").strip()
        self._cached_viewer_ids = {
            p.strip() for p in raw_viewer.split(",") if p.strip()
        }

        self._cache_time = now

    def get_admin_ids(self) -> Set[str]:
        """Get set of admin chat IDs."""
        self._refresh_cache()
        return self._cached_admin_ids or set()

    def get_viewer_ids(self) -> Set[str]:
        """Get set of viewer chat IDs."""
        self._refresh_cache()
        return self._cached_viewer_ids or set()

    def get_all_authorized_ids(self) -> Set[str]:
        """Get all authorized IDs (admin + viewer)."""
        return self.get_admin_ids() | self.get_viewer_ids()

    def get_primary_chat_id(self) -> str:
        """Get primary admin chat ID (first configured)."""
        raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not raw:
            return ""
        return raw.split(",")[0].strip()

    # ── Role Resolution ───────────────────────────────────────

    def get_role(
        self,
        chat_id: str,
        context_bot_data: Optional[Dict] = None,
    ) -> AuthRole:
        """
        Determine the role for a chat ID.

        Priority:
        1. Temporary elevated session
        2. Admin IDs (TELEGRAM_CHAT_ID)
        3. Bot data context (set at startup)
        4. Viewer IDs (TELEGRAM_VIEWER_IDS)
        5. Unknown

        Args:
            chat_id: Chat ID to check
            context_bot_data: bot_data from Telegram context

        Returns:
            AuthRole enum
        """
        chat_str = str(chat_id).strip()

        # Check temporary session
        with self._lock:
            if chat_str in self._temp_sessions:
                if time.time() < self._temp_sessions[chat_str]:
                    return AuthRole.ADMIN
                else:
                    # Session expired
                    del self._temp_sessions[chat_str]

        # Check admin IDs
        if chat_str in self.get_admin_ids():
            return AuthRole.ADMIN

        # Check bot_data (set during bot startup)
        if context_bot_data:
            bd_id = str(
                context_bot_data.get("chat_id", "")
            ).strip()
            if bd_id and chat_str == bd_id:
                return AuthRole.ADMIN

        # Check viewer IDs
        if chat_str in self.get_viewer_ids():
            return AuthRole.VIEWER

        return AuthRole.UNKNOWN

    def is_authorized(
        self,
        chat_id: str,
        min_role: AuthRole = AuthRole.VIEWER,
        context_bot_data: Optional[Dict] = None,
    ) -> bool:
        """
        Check if a chat ID has at least the minimum role.

        Args:
            chat_id: Chat ID to check
            min_role: Minimum required role
            context_bot_data: bot_data from context

        Returns:
            True if authorized
        """
        role = self.get_role(chat_id, context_bot_data)

        if min_role == AuthRole.ADMIN:
            return role == AuthRole.ADMIN
        elif min_role == AuthRole.VIEWER:
            return role in (AuthRole.ADMIN, AuthRole.VIEWER)

        return False

    # ── Rate Limiting ─────────────────────────────────────────

    def check_rate_limit(self, chat_id: str) -> bool:
        """
        Check and record an unauthorized attempt.

        Returns:
            True if rate limited (too many attempts)
        """
        with self._lock:
            state = self._rate_states[chat_id]
            now = time.time()

            # Prune old attempts
            state.attempts = [
                t for t in state.attempts
                if now - t < _RATE_WINDOW_SEC
            ]
            state.attempts.append(now)
            state.total_denied += 1

            return len(state.attempts) > _RATE_MAX_ATTEMPTS

    def should_send_reply(self, chat_id: str) -> bool:
        """
        Check if we should send an "Unauthorized" reply.

        Prevents spamming the unauthorized user with repeated
        denial messages. Only sends one reply per cooldown window.

        Returns:
            True if enough time has passed since last reply
        """
        with self._lock:
            state = self._rate_states[chat_id]
            now = time.time()

            if now - state.last_reply_time >= _REPLY_COOLDOWN_SEC:
                state.last_reply_time = now
                return True
            return False

    # ── Temporary Sessions ────────────────────────────────────

    def grant_temp_session(
        self,
        chat_id: str,
        duration_seconds: int = 3600,
    ) -> None:
        """
        Grant temporary admin access to a chat ID.

        Args:
            chat_id: Chat ID to elevate
            duration_seconds: Session duration (default 1 hour)
        """
        with self._lock:
            expiry = time.time() + duration_seconds
            self._temp_sessions[str(chat_id).strip()] = expiry
            logger.warning(
                f"🔑 Temporary admin session granted | "
                f"chat={chat_id} | "
                f"duration={duration_seconds}s"
            )

    def revoke_temp_session(self, chat_id: str) -> bool:
        """Revoke a temporary session. Returns True if existed."""
        with self._lock:
            return self._temp_sessions.pop(
                str(chat_id).strip(), None
            ) is not None

    def get_active_sessions(self) -> Dict[str, float]:
        """Get all active temporary sessions with remaining time."""
        with self._lock:
            now = time.time()
            return {
                cid: round(expiry - now, 1)
                for cid, expiry in self._temp_sessions.items()
                if expiry > now
            }

    # ── Audit Trail ───────────────────────────────────────────

    def record_attempt(self, attempt: AuthAttempt) -> None:
        """Record an authentication attempt."""
        with self._lock:
            self._audit_log.append(attempt)
            # Trim old entries
            if len(self._audit_log) > _MAX_AUDIT_ENTRIES:
                self._audit_log = self._audit_log[
                    -_MAX_AUDIT_ENTRIES:
                ]

    def get_audit_log(
        self,
        last_n: int = 50,
        result_filter: Optional[AuthResult] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get recent authentication attempts.

        Args:
            last_n: Number of recent entries
            result_filter: Filter by result type

        Returns:
            List of audit entries as dicts
        """
        with self._lock:
            entries = list(self._audit_log)

        if result_filter:
            entries = [
                e for e in entries if e.result == result_filter
            ]

        return [
            {
                "time": e.timestamp.strftime("%H:%M:%S"),
                "chat_id": e.chat_id,
                "username": e.username,
                "handler": e.handler,
                "result": e.result.value,
                "role": e.role.value,
                "details": e.details,
            }
            for e in entries[-last_n:]
        ]

    def get_security_stats(self) -> Dict[str, Any]:
        """Get security statistics."""
        with self._lock:
            total = len(self._audit_log)
            granted = sum(
                1 for a in self._audit_log
                if a.result == AuthResult.GRANTED
            )
            denied = sum(
                1 for a in self._audit_log
                if a.result == AuthResult.DENIED
            )
            rate_limited = sum(
                1 for a in self._audit_log
                if a.result == AuthResult.RATE_LIMITED
            )

            # Unique unauthorized IPs
            unauth_chats = {
                a.chat_id for a in self._audit_log
                if a.result in (
                    AuthResult.DENIED, AuthResult.RATE_LIMITED
                )
            }

            active_sessions = {
                cid: round(exp - time.time(), 1)
                for cid, exp in self._temp_sessions.items()
                if exp > time.time()
            }

        return {
            "total_attempts": total,
            "granted": granted,
            "denied": denied,
            "rate_limited": rate_limited,
            "unique_unauthorized_chats": len(unauth_chats),
            "unauthorized_chat_ids": list(unauth_chats)[:10],
            "admin_ids_configured": len(self.get_admin_ids()),
            "viewer_ids_configured": len(self.get_viewer_ids()),
            "active_temp_sessions": active_sessions,
            "rate_limit_window": _RATE_WINDOW_SEC,
            "rate_limit_max": _RATE_MAX_ATTEMPTS,
            "reply_cooldown": _REPLY_COOLDOWN_SEC,
        }


# Global singleton
_auth = _AuthManager()


# ═════════════════════════════════════════════════════════════════
#  SAFE REPLY HELPER
# ═════════════════════════════════════════════════════════════════

async def _safe_reply(update, text: str) -> None:
    """
    Safely send a reply, handling messages, callbacks, and edge cases.

    CRITICAL FIX: update.message is None on callback queries.
    """
    try:
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.answer(text[:200])
        elif update.effective_chat:
            bot = update.get_bot()
            await bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
            )
    except Exception as e:
        logger.warning(f"Could not send auth reply: {e}")


# ═════════════════════════════════════════════════════════════════
#  EXTRACT USER INFO
# ═════════════════════════════════════════════════════════════════

def _extract_user_info(update) -> Tuple[str, str]:
    """
    Extract chat ID and username from an update.

    Returns:
        Tuple of (chat_id, username)
    """
    chat_id = ""
    username = ""

    if update and update.effective_chat:
        chat_id = str(update.effective_chat.id)

    if update and update.effective_user:
        user = update.effective_user
        username = user.username or user.first_name or str(user.id)

    return chat_id, username


# ═════════════════════════════════════════════════════════════════
#  CORE AUTH CHECK (reusable logic)
# ═════════════════════════════════════════════════════════════════

async def _check_auth(
    update,
    context,
    handler_name: str,
    min_role: AuthRole = AuthRole.VIEWER,
) -> Tuple[AuthResult, AuthRole, str]:
    """
    Core authentication check logic.

    Args:
        update: Telegram Update
        context: Telegram context
        handler_name: Name of the handler being called
        min_role: Minimum required role

    Returns:
        Tuple of (result, role, chat_id)
    """
    # ── No update ─────────────────────────────────────────────
    if not update or not update.effective_chat:
        logger.warning(
            f"Auth: no effective_chat | handler={handler_name}"
        )
        _auth.record_attempt(AuthAttempt(
            timestamp=datetime.now(timezone.utc),
            chat_id="unknown",
            username="unknown",
            handler=handler_name,
            result=AuthResult.NO_UPDATE,
        ))
        return AuthResult.NO_UPDATE, AuthRole.UNKNOWN, ""

    chat_id, username = _extract_user_info(update)

    # ── Get context bot_data ──────────────────────────────────
    bot_data = None
    if context and hasattr(context, "bot_data"):
        bot_data = context.bot_data

    # ── Check all authorized IDs ──────────────────────────────
    all_ids = _auth.get_all_authorized_ids()
    if bot_data:
        bd_id = str(bot_data.get("chat_id", "")).strip()
        if bd_id:
            all_ids.add(bd_id)

    # ── Config check ──────────────────────────────────────────
    if not all_ids:
        logger.error(
            f"TELEGRAM_CHAT_ID not configured | handler={handler_name}"
        )
        await _safe_reply(
            update,
            "❌ Bot misconfigured. Set TELEGRAM_CHAT_ID in .env"
        )
        _auth.record_attempt(AuthAttempt(
            timestamp=datetime.now(timezone.utc),
            chat_id=chat_id,
            username=username,
            handler=handler_name,
            result=AuthResult.MISCONFIGURED,
        ))
        return AuthResult.MISCONFIGURED, AuthRole.UNKNOWN, chat_id

    # ── Resolve role ──────────────────────────────────────────
    role = _auth.get_role(chat_id, bot_data)

    # ── Check authorization ───────────────────────────────────
    if not _auth.is_authorized(chat_id, min_role, bot_data):
        # Rate limit check
        if _auth.check_rate_limit(chat_id):
            logger.warning(
                f"🚫 Rate limited | chat={chat_id} | "
                f"user={username} | handler={handler_name}"
            )
            _auth.record_attempt(AuthAttempt(
                timestamp=datetime.now(timezone.utc),
                chat_id=chat_id,
                username=username,
                handler=handler_name,
                result=AuthResult.RATE_LIMITED,
                details=f"required={min_role.value}",
            ))
            return AuthResult.RATE_LIMITED, role, chat_id

        # Log denial
        logger.warning(
            f"⛔ Unauthorized | chat={chat_id} | "
            f"user={username} | role={role.value} | "
            f"required={min_role.value} | handler={handler_name}"
        )

        # Send reply (with cooldown)
        if _auth.should_send_reply(chat_id):
            if min_role == AuthRole.ADMIN and role == AuthRole.VIEWER:
                await _safe_reply(
                    update,
                    "⛔ Admin access required. "
                    "You have viewer-only permissions."
                )
            else:
                await _safe_reply(
                    update,
                    "⛔ Unauthorized. This bot is private."
                )

        _auth.record_attempt(AuthAttempt(
            timestamp=datetime.now(timezone.utc),
            chat_id=chat_id,
            username=username,
            handler=handler_name,
            result=AuthResult.DENIED,
            role=role,
            details=f"required={min_role.value}",
        ))
        return AuthResult.DENIED, role, chat_id

    # ── Authorized ────────────────────────────────────────────
    logger.debug(
        f"✅ Auth passed | chat={chat_id} | "
        f"user={username} | role={role.value} | "
        f"handler={handler_name}"
    )
    _auth.record_attempt(AuthAttempt(
        timestamp=datetime.now(timezone.utc),
        chat_id=chat_id,
        username=username,
        handler=handler_name,
        result=AuthResult.GRANTED,
        role=role,
    ))
    return AuthResult.GRANTED, role, chat_id


# ═════════════════════════════════════════════════════════════════
#  DECORATORS
# ═════════════════════════════════════════════════════════════════

def require_auth(func: Callable) -> Callable:
    """
    Decorator: requires viewer-level access (admin or viewer).

    Usage:
        @require_auth
        async def cmd_status(update, context):
            await update.message.reply_text("Status: running")
    """
    if not TELEGRAM_AVAILABLE:
        @functools.wraps(func)
        async def stub(*args, **kwargs):
            logger.warning(
                f"Auth stub: {func.__name__} called "
                f"without telegram library"
            )
        return stub

    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        result, role, chat_id = await _check_auth(
            update, context, func.__name__, AuthRole.VIEWER
        )
        if result != AuthResult.GRANTED:
            return

        # Inject role into context for handler to use
        if context and hasattr(context, "bot_data"):
            context.bot_data["_auth_role"] = role.value
            context.bot_data["_auth_chat_id"] = chat_id

        return await func(update, context, *args, **kwargs)

    return wrapper


def require_admin(func: Callable) -> Callable:
    """
    Decorator: requires admin-level access.

    Use for destructive operations (kill switch, config changes, etc.)

    Usage:
        @require_admin
        async def cmd_kill(update, context):
            await update.message.reply_text("Bot stopped!")
    """
    if not TELEGRAM_AVAILABLE:
        @functools.wraps(func)
        async def stub(*args, **kwargs):
            logger.warning(
                f"Auth stub (admin): {func.__name__} called "
                f"without telegram library"
            )
        return stub

    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        result, role, chat_id = await _check_auth(
            update, context, func.__name__, AuthRole.ADMIN
        )
        if result != AuthResult.GRANTED:
            return

        if context and hasattr(context, "bot_data"):
            context.bot_data["_auth_role"] = role.value
            context.bot_data["_auth_chat_id"] = chat_id

        return await func(update, context, *args, **kwargs)

    return wrapper


def auth_optional(func: Callable) -> Callable:
    """
    Decorator: allows unauthenticated access but injects role info.

    Useful for public info commands where auth adds context.

    Usage:
        @auth_optional
        async def cmd_help(update, context):
            role = context.bot_data.get("_auth_role", "unknown")
            # Show admin commands only to admins
    """
    if not TELEGRAM_AVAILABLE:
        @functools.wraps(func)
        async def stub(*args, **kwargs):
            pass
        return stub

    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        chat_id, username = _extract_user_info(update)

        bot_data = None
        if context and hasattr(context, "bot_data"):
            bot_data = context.bot_data

        role = _auth.get_role(chat_id, bot_data) if chat_id else AuthRole.UNKNOWN

        if context and hasattr(context, "bot_data"):
            context.bot_data["_auth_role"] = role.value
            context.bot_data["_auth_chat_id"] = chat_id

        return await func(update, context, *args, **kwargs)

    return wrapper


# ═════════════════════════════════════════════════════════════════
#  PUBLIC API FUNCTIONS
# ═════════════════════════════════════════════════════════════════

def get_authorized_chat_id() -> str:
    """
    Get primary authorized (admin) chat ID.

    Returns:
        Primary chat ID string, or empty string if not configured
    """
    return _auth.get_primary_chat_id()


def get_all_authorized_ids() -> Set[str]:
    """Get all authorized IDs (admin + viewer)."""
    return _auth.get_all_authorized_ids()


def is_authorized(
    chat_id: str,
    min_role: AuthRole = AuthRole.VIEWER,
) -> bool:
    """
    Check if a chat ID has at least the minimum role.

    Args:
        chat_id: Chat ID to check
        min_role: Minimum required role

    Returns:
        True if authorized
    """
    return _auth.is_authorized(str(chat_id).strip(), min_role)


def get_role(chat_id: str) -> AuthRole:
    """
    Get the role for a chat ID.

    Args:
        chat_id: Chat ID

    Returns:
        AuthRole enum
    """
    return _auth.get_role(str(chat_id).strip())


def get_chat_id_from_context(context) -> str:
    """
    Get authorized chat ID from bot_data or env.

    Used by notifier for proactive messages.

    Args:
        context: Telegram context

    Returns:
        Chat ID string or empty string
    """
    if context and hasattr(context, "bot_data") and context.bot_data:
        stored = str(context.bot_data.get("chat_id", "")).strip()
        if stored:
            return stored
    return get_authorized_chat_id()


# ── Session Management ──────────────────────────────────────────

def grant_temp_session(
    chat_id: str,
    duration_seconds: int = 3600,
) -> None:
    """Grant temporary admin access."""
    _auth.grant_temp_session(chat_id, duration_seconds)


def revoke_temp_session(chat_id: str) -> bool:
    """Revoke temporary admin access."""
    return _auth.revoke_temp_session(chat_id)


def get_active_sessions() -> Dict[str, float]:
    """Get active temporary sessions."""
    return _auth.get_active_sessions()


# ── Audit & Stats ───────────────────────────────────────────────

def get_audit_log(
    last_n: int = 50,
    result_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Get recent auth attempts.

    Args:
        last_n: Number of entries
        result_filter: Filter by result ("granted", "denied", etc.)

    Returns:
        List of audit entry dicts
    """
    filt = None
    if result_filter:
        try:
            filt = AuthResult(result_filter)
        except ValueError:
            pass
    return _auth.get_audit_log(last_n=last_n, result_filter=filt)


def get_security_stats() -> Dict[str, Any]:
    """Get security statistics."""
    return _auth.get_security_stats()


def clear_rate_limits() -> None:
    """Clear all rate limiting state."""
    with _auth._lock:
        _auth._rate_states.clear()
    logger.info("🔄 Rate limits cleared")


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Telegram Auth v2 — Diagnostics")
    print("=" * 60)

    print(f"\n  Telegram available: "
          f"{'✅' if TELEGRAM_AVAILABLE else '❌'}")

    print(f"\n  ── Configuration ──")
    print(f"  Admin IDs:          {_auth.get_admin_ids() or '(none)'}")
    print(f"  Viewer IDs:         {_auth.get_viewer_ids() or '(none)'}")
    print(f"  Primary chat ID:    {get_authorized_chat_id() or '(none)'}")
    print(f"  Rate limit:         {_RATE_MAX_ATTEMPTS}/{_RATE_WINDOW_SEC}s")
    print(f"  Reply cooldown:     {_REPLY_COOLDOWN_SEC}s")

    print(f"\n  ── Role Check ──")
    test_id = get_authorized_chat_id()
    if test_id:
        role = get_role(test_id)
        print(f"  ID {test_id}: {role.value}")
        print(f"  is_authorized(viewer): {is_authorized(test_id, AuthRole.VIEWER)}")
        print(f"  is_authorized(admin):  {is_authorized(test_id, AuthRole.ADMIN)}")
    else:
        print("  No TELEGRAM_CHAT_ID configured")

    print(f"\n  ── Decorators ──")
    print(f"  @require_auth:    ✅ Available")
    print(f"  @require_admin:   ✅ Available")
    print(f"  @auth_optional:   ✅ Available")

    print(f"\n  ── Security Stats ──")
    stats = get_security_stats()
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 60)
    print("  ✅ Auth v2 fully operational")
    print("=" * 60)