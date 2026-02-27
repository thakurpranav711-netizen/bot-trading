# app/telegram/auth.py

import os
from dotenv import load_dotenv
from telegram import Update
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Load env ONCE, from correct location
load_dotenv("config/.env")


def _load_allowed_users():
    """
    Loads allowed Telegram user IDs from env.

    ENV:
    TELEGRAM_ALLOWED_USERS=123,456

    If NOT set:
    → AUTH DISABLED (dev mode)
    → All users allowed
    """

    user_ids = os.getenv("TELEGRAM_ALLOWED_USERS")

    if not user_ids:
        logger.warning(
            "⚠️ TELEGRAM_ALLOWED_USERS not set — AUTH DISABLED (DEV MODE)"
        )
        return None  # None = allow all users

    allowed = set()

    for uid in user_ids.split(","):
        uid = uid.strip()
        try:
            allowed.add(int(uid))
        except ValueError:
            logger.warning(f"⚠️ Invalid Telegram user ID in env: {uid}")

    if not allowed:
        logger.error("❌ TELEGRAM_ALLOWED_USERS parsed but EMPTY")
    else:
        logger.info(f"✅ Allowed Telegram users loaded: {allowed}")

    return allowed


# Load once at import time
ALLOWED_USERS = _load_allowed_users()


def is_authorized(update: Update) -> bool:
    """
    Authorization check for Telegram updates.
    """

    if not update or not update.effective_user:
        logger.warning("⛔ Update or effective_user missing")
        return False

    user_id = update.effective_user.id

    # DEV MODE → allow all users
    if ALLOWED_USERS is None:
        logger.debug(f"✅ DEV MODE: allowing user {user_id}")
        return True

    if user_id in ALLOWED_USERS:
        logger.info(f"✅ Authorized user: {user_id}")
        return True

    logger.warning(
        f"⛔ Unauthorized user {user_id} | Allowed: {ALLOWED_USERS}"
    )
    return False